from app import app, get_db, staff_required, usuario_required, insertar_y_obtener_id
from compras import register
register(app, get_db, staff_required, usuario_required, insertar_y_obtener_id)
