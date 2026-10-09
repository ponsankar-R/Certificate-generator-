import logging
import os
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from certificate_service import (
    CertificateRequestError,
    extract_certificate_id,
    generate_certificates_zip,
)
from database import Base, SessionLocal, engine
from models import Certificate

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("certificate-generator")

# Create database tables automatically
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Bulk Certificate Generator API",
    description="Backend API for generating and verifying certificates.",
    version="1.0.0",
)

# Parse CORS origins or default to allowing all
raw_origins = os.getenv("CORS_ORIGINS", "*")
origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if "*" in origins else origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # Required so external frontends can inspect the downloaded filename header
    expose_headers=["Content-Disposition"],
)


class VerifyRequest(BaseModel):
    value: Optional[str] = None
    code: Optional[str] = None


@app.get("/")
def root():
    return {
        "status": "online",
        "message": "Bulk Certificate Generator API is running.",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate-certificates")
def generate_certificates(
    title: str = Form(...),
    default_date: Optional[str] = Form(None),
    file: UploadFile = File(...),
):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file.")

    csv_bytes = file.file.read()

    try:
        zip_buffer = generate_certificates_zip(
            title=title,
            default_date=default_date,
            csv_bytes=csv_bytes,
        )
    except CertificateRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Certificate generation failed")
        raise HTTPException(
            status_code=500,
            detail="Certificate generation failed.",
        )

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="certificates.zip"'
        },
    )


def _verify_value(value: str):
    certificate_id = extract_certificate_id(value)

    if not certificate_id:
        return JSONResponse(
            status_code=400,
            content={
                "verified": False,
                "message": "Invalid certificate code or QR value.",
            },
        )

    db = SessionLocal()
    try:
        certificate = (
            db.query(Certificate)
            .filter(
                Certificate.id == certificate_id,
                Certificate.status == "generated",
            )
            .first()
        )
    finally:
        db.close()

    if not certificate:
        return JSONResponse(
            status_code=404,
            content={
                "verified": False,
                "message": "Certificate not found.",
                "certificate_id": certificate_id,
            },
        )

    return {
        "verified": True,
        "message": "Verified certificate",
        "certificate": {
            "id": certificate.id,
            "name": certificate.candidate_name,
            "title": certificate.title,
            "date": certificate.certificate_date.isoformat(),
            "created_at": certificate.created_at.isoformat()
            if certificate.created_at
            else None,
        },
    }


@app.get("/verify/{value:path}")
def verify_get(value: str):
    return _verify_value(value)


@app.post("/verify")
def verify_post(payload: VerifyRequest):
    value = payload.value or payload.code or ""
    return _verify_value(value)