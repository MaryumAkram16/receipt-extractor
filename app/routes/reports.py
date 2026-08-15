import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"

# Filenames are always generated server-side as spending-report-<10 hex chars>.pdf
# — this pattern rejects anything else, including path-traversal attempts,
# before it ever touches the filesystem.
_SAFE_FILENAME = re.compile(r"^[a-zA-Z0-9_-]+\.pdf$")


@router.get("/reports/{filename}")
def get_report(filename: str):
    if not _SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail="invalid report filename")
    filepath = REPORTS_DIR / filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="no report with that filename")
    return FileResponse(filepath, media_type="application/pdf", filename=filename)