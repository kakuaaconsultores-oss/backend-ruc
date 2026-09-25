from app import app, get_db, staff_required, usuario_required, insertar_y_obtener_id
from compras import register
register(app, get_db, staff_required, usuario_required, insertar_y_obtener_id)
\nfrom multiempresa import register as register_multiempresa\nregister_multiempresa(app, get_db, staff_required, admin_required, obtener_usuario_por_token)\n