from app import app, get_db, staff_required, usuario_required, admin_required, insertar_y_obtener_id
from compras import register
register(app, get_db, staff_required, usuario_required, insertar_y_obtener_id)

from multiempresa import register as register_multiempresa
register_multiempresa(app, get_db, staff_required, admin_required, obtener_usuario_por_token)
