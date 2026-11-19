import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..reports import generator

router = APIRouter()

# Filenames are always generated server-side as spending-report-<hex>.pdf
# (10 hex chars for reports made by older versions, 32 for current ones).
# This pattern rejects anything else, including path-traversal attempts,
# before it ever touches the filesystem.
_SAFE_FILENAME = re.compile(r"^spending-report-[0-9a-f]{10,32}\.pdf$")


@router.get("/reports/{filename}")
def get_report(filename: str):
    if not _SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(status_code=400, detail="invalid report filename")
    reports_dir = generator.REPORTS_DIR.resolve()
    filepath = (reports_dir / filename).resolve()
    # Defence in depth: even with the regex above, refuse anything that
    # resolves outside the reports directory (e.g. via a planted symlink).
    if filepath.parent != reports_dir or not filepath.is_file():
        raise HTTPException(status_code=404, detail="no report with that filename")
    return FileResponse(filepath, media_type="application/pdf", filename=filename)
