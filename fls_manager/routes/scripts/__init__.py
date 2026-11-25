from flask import Blueprint

bp = Blueprint("scripts", __name__)

from . import pages
from . import files
from . import pull
from . import debug
from . import native_files
from . import native_pull
