# Bulk Certificate Generator

A backend service and decoupled web application designed to process bulk certificate generation requests for a list of recipients, validate candidate information, dynamically render PDF certificates using a predefined template, and stream a consolidated ZIP archive. The service also provides persistent UUID verification via embedded QR codes.

---

## Live Deployments

* **Frontend Web Application:** https://certificate-gen-01.netlify.app/
* **Backend API Base:** https://certificate-generator-ccym.onrender.com
* **Interactive API Documentation:** https://certificate-generator-ccym.onrender.com/docs

---

## Technology Stack

* **Programming Language:** Python 3.12+ / Python 3.14
* **API Framework:** FastAPI
* **Database & ORM:** PostgreSQL (Neon Serverless PostgreSQL), SQLAlchemy 2.0
* **Database Driver:** `psycopg` (v3 binary)
* **PDF Engine:** ReportLab
* **QR Engine:** `qrcode[pil]`
* **ASGI Server:** Uvicorn
* **Frontend:** Vanilla HTML5, CSS3, JavaScript (Fetch API)

---

## Project Structure

```text
certificate_gen/
├── app/
│   ├── app.js               # Frontend fetch calls and automatic ZIP download handler
│   ├── index.html           # Generation and verification forms
│   └── styles.css           # Responsive stylesheet
├── certificate_service.py   # Candidate validation, PDF rendering, and ZIP compilation logic
├── database.py              # Database engine and session lifecycle configuration
├── main.py                  # API routes, CORS setup, and endpoint handlers
├── models.py                # Database models (Certificate schema)
├── requirements.txt         # Project dependencies
└── README.md                # System documentation

```

---

## Setup and Installation

### 1. Prerequisites

* Python 3.10+ installed locally.
* A PostgreSQL database instance (such as Neon PostgreSQL).
* Git installed.

### 2. Clone the Repository

```bash
git clone https://github.com/<your-username>/certificate-generator-backend.git
cd certificate-generator-backend

```

### 3. Create and Activate Virtual Environment

```bash
# Linux / macOS:
python3 -m venv venv
source venv/bin/activate

# Windows (PowerShell):
python -m venv venv
.\venv\Scripts\Activate.ps1

```

### 4. Install Dependencies

```bash
pip install -r requirements.txt

```

### 5. Environment Variables

Create a `.env` file in the project root:

```env
# Database connection string
DATABASE_URL=postgresql://<user>:<password>@<host>/<database>?sslmode=require

# Base URL embedded inside certificate verification QR codes
PUBLIC_BASE_URL=http://127.0.0.1:8000

# Allowed CORS origins
CORS_ORIGINS=*

# Concurrency and safety limits
MAX_CSV_ROWS=5000
MAX_WORKERS=4

```

---

## How to Run the Application

### Local Development

Run the ASGI server with hot-reload enabled:

```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000

```

* **Swagger Documentation:** Access [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).
* **Frontend UI:** Open `app/index.html` in your browser.

### Production Execution (Render)

Run the server bound to the container port:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT --workers 2

```

---

## Running Automated Tests

The test suite validates input parsing, single-row fault tolerance, database persistence, and certificate verification.

```bash
# Install test dependencies
pip install pytest httpx

# Run tests
pytest -v

```

### Test Coverage

1. **Input Validation:** Verifies that payloads missing the `Candidate Name` attribute or non-CSV/invalid formats return `400 Bad Request`.
2. **Batch Generation:** Validates that recipient requests return a valid `200 OK` response with `Content-Type: application/zip`.
3. **Failure Isolation:** Verifies that a record with an invalid or empty name is flagged without stopping generation for remaining valid recipients.
4. **Certificate Lookup:** Confirms valid UUID lookups return stored candidate records and non-existent IDs return `404 Not Found`.

---

## Submitting Certificate Generation Requests

Clients submit generation requests in bulk using a single API call.

* **Endpoint:** `POST /generate-certificates`
* **Content-Type:** `multipart/form-data` (or `application/json`)

### Form / Payload Fields

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `title` | string | Yes | Title of the event, workshop, or course (max 200 chars) |
| `file` / `recipients` | File / JSON | Yes | List of recipients containing only `Candidate Name` |

### Recipient Data Format

Only the **`Candidate Name`** is required per recipient. No date parameters are needed:

#### JSON Format:

```json
{
  "title": "FastAPI Backend Workshop",
  "recipients": [
    { "Candidate Name": "Jane Doe" },
    { "Candidate Name": "John Smith" },
    { "Candidate Name": "Alex Johnson" }
  ]
}

```

#### CSV Format:

```csv
Candidate Name
Jane Doe
John Smith
Alex Johnson

```

### Example Submission via `cURL`:

```bash
curl -X POST "http://127.0.0.1:8000/generate-certificates" \
     -F "title=FastAPI Backend Workshop" \
     -F "file=@candidates.csv" \
     --output certificates.zip

```

---

## Retrieving and Verifying Certificates

### 1. File Download

* The API streams a ZIP file directly via `Content-Disposition: attachment; filename="certificates.zip"`.
* The web application consumes the binary stream and triggers an immediate download in the user's browser.

### 2. Verification Lookup

Every generated certificate has an embedded UUID and QR code pointing to verification endpoints:

* **GET:** `GET /verify/{certificate_id}`
* **POST:** `POST /verify` with payload `{"value": "<UUID-or-scanned-QR-text>"}`

#### Verification Response:

```json
{
  "verified": true,
  "message": "Verified certificate",
  "certificate": {
    "id": "b3e390c5-e51c-4b58-b649-0d125270bc55",
    "name": "Jane Doe",
    "title": "FastAPI Backend Workshop",
    "created_at": "2026-10-09T12:00:00"
  }
}

```

---

## Important Implementation and Design Decisions

### 1. In-Memory Streaming Architecture

Generated PDFs are written into memory buffers (`io.BytesIO`) and bundled into an in-memory ZIP file rather than saved to disk. This prevents disk storage leaks on ephemeral cloud hosts like Render and ensures rapid response delivery.

### 2. Simplified Schema (Candidate Name Only)

Requiring strictly `Candidate Name` eliminates validation failures caused by inconsistent date formats (e.g., `DD/MM/YYYY` vs `YYYY-MM-DD`). Creation timestamps (`created_at`) are handled automatically by the database layer.

### 3. Fault-Tolerant Row Isolation

Each recipient record is validated independently. If a record is empty or exceeds the maximum length (40 characters), the system records the individual failure without interrupting generation for the remaining recipients.

### 4. Direct Synchronous Processing

For typical batch sizes (up to hundreds of recipients), in-memory generation completes within seconds. Processing synchronously avoids the operational overhead and maintenance of Redis queues or Celery worker pools.
