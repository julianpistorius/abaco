from flask import Flask
from flask_cors import CORS

from agaveflask.utils import AgaveApi, handle_error

from auth import authn_and_authz
from controllers import PermissionsResource, WorkersResource, WorkerResource

app = Flask(__name__)
CORS(app)
api = AgaveApi(app)

# Authn/z
@app.before_request
def auth():
    authn_and_authz()

# Set up error handling
@app.errorhandler(Exception)
def handle_all_errors(e):
    return handle_error(e)

# Resources
api.add_resource(WorkersResource, '/actors/<string:actor_id>/workers')
api.add_resource(PermissionsResource, '/actors/<string:actor_id>/permissions')
api.add_resource(WorkerResource, '/actors/<string:actor_id>/workers/<string:worker_id>')

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
