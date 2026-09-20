from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)  # Esto permite que tu página HTML consulte al backend

@app.route('/')
def home():
    return "API de consulta de RUC funcionando"

@app.route('/api/ruc')
def buscar_ruc():
    ruc = request.args.get('ruc')
    if not ruc:
        return jsonify({"error": "Falta el parámetro 'ruc'"}), 400

    try:
        # Consulta a la API de TuRuc
        resp = requests.get(
            f'https://turuc.com.py/api/contribuyente/{ruc}',
            timeout=10
        )
        return jsonify(resp.json())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run()
