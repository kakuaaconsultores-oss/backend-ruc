from app import app, get_db, staff_required, usuario_required
from compras import register
register(app, get_db, staff_required, usuario_required)
