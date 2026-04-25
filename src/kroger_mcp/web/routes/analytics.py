"""Analytics & Reports page route."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    # Pre-load a 30-day spending summary for initial render
    report = {}
    try:
        from kroger_mcp.analytics.reporting import generate_spending_report

        report = generate_spending_report(days_back=30)
        # Normalise key name: function returns 'by_category', template expects
        # 'category_breakdown'
        if "by_category" in report and "category_breakdown" not in report:
            report["category_breakdown"] = report["by_category"]
        # Ensure report is JSON-serializable (convert sets to lists)
        if "category_breakdown" in report:
            for item in report["category_breakdown"]:
                if "products" in item and isinstance(item["products"], set):
                    item["products"] = list(item["products"])
    except Exception:
        pass

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "active_page": "analytics",
            "initial_report": report,
        },
    )
