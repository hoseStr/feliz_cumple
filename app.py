# Servidor Flask del ramo de lirios ASCII. Vercel detecta `app` en app.py sin configuración.
# La animación está en public/ramo_de_flores.py y corre en el navegador (PyScript);
# en Vercel, la carpeta public/ la sirve directamente su CDN.
from flask import Flask, render_template, send_from_directory

app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    # en local no hay CDN: Flask sirve también los archivos de public/
    @app.get("/<path:archivo>")
    def publico(archivo):
        return send_from_directory("public", archivo)

    app.run(debug=True)
