from flask import Blueprint

bp = Blueprint("tasks", __name__)

# 导入子模块，让 @bp.route 生效
from . import pages
from . import actions
from . import config_file
from . import logs
from . import collections