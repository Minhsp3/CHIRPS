from __future__ import annotations

import sys
from pathlib import Path


DEPLOY_DIR = Path(__file__).resolve().parent / "streamlit_deploy"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from api_dashboard import main


main()
