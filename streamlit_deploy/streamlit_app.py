from __future__ import annotations

import runpy
import sys
from pathlib import Path

import streamlit as st


APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from api_dashboard import main


HYDROLOGY_PAGE = APP_DIR / "truyvan_thuyvan" / "streamlit_app.py"


def reservoir_dashboard() -> None:
    main()


def hydrology_query() -> None:
    page_dir = HYDROLOGY_PAGE.parent
    if str(page_dir) not in sys.path:
        sys.path.insert(0, str(page_dir))
    runpy.run_path(str(HYDROLOGY_PAGE), run_name="hydroai_hydrology_query")


st.set_page_config(
    page_title="HydroAI",
    page_icon=":material/water_drop:",
    layout="wide",
)

page = st.navigation(
    [
        st.Page(
            reservoir_dashboard,
            title="Dashboard hồ chứa",
            icon=":material/water_drop:",
            url_path="ho-chua",
            default=True,
        ),
        st.Page(
            hydrology_query,
            title="Truy vấn thủy văn",
            icon=":material/query_stats:",
            url_path="truy-van-thuy-van",
        ),
    ],
    position="top",
)
page.run()
