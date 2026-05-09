from flask import Blueprint

bp = Blueprint("about", __name__)

from . import page
from . import version
from . import jobs
from . import panel_control
from . import time_sync