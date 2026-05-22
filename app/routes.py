from flask import Blueprint, render_template, request, redirect, url_for, flash
from .analysis import run_analysis
import io

bp = Blueprint("main", __name__)


@bp.route("/", methods=["GET"])
def index():
    return render_template("upload.html")


@bp.route("/analyze", methods=["POST"])
def analyze():
    if "report" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))

    f = request.files["report"]
    if not f.filename or not f.filename.lower().endswith(".csv"):
        flash("Please upload a .csv file.", "error")
        return redirect(url_for("main.index"))

    try:
        content = f.read()
        result = run_analysis(io.BytesIO(content))
        return render_template("results.html", **result)
    except Exception as e:
        flash(f"Could not parse report: {e}", "error")
        return redirect(url_for("main.index"))
