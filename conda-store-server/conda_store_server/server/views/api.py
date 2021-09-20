from flask import Blueprint, jsonify, redirect, request
import pydantic
from typing import List, Dict

from conda_store_server import api, orm, schema, utils
from conda_store_server.server.utils import get_conda_store, get_auth, get_server
from conda_store_server.server.auth import Permissions


app_api = Blueprint("api", __name__)


def get_limit_offset_args(request):
    server = get_server()

    page = int(request.args.get("page", 1))
    size = min(
        int(request.args.get("size", server.max_page_size)), server.max_page_size
    )
    offset = (page - 1) * size
    return size, offset


def get_sorts(
    request,
    allowed_sort_bys: Dict = {},
    default_sort_by: List = [],
    default_order: str = "asc",
):
    sort_by = request.args.getlist("sort_by") or default_sort_by
    sort_by = [allowed_sort_bys[s] for s in sort_by if s in allowed_sort_bys]

    order = request.args.get("order", default_order)
    if order not in {"asc", "desc"}:
        order = default_order

    order_mapping = {"asc": lambda c: c.asc(), "desc": lambda c: c.desc()}
    return [order_mapping[order](k) for k in sort_by]


def paginated_api_response(
    query,
    object_schema,
    sorts: List = [],
    exclude=None,
    allowed_sort_bys: Dict = {},
    default_sort_by: List = [],
    default_order: str = "asc",
):

    limit, offset = get_limit_offset_args(request)
    sorts = get_sorts(
        request,
        allowed_sort_bys,
        default_sort_by=default_sort_by,
        default_order=default_order,
    )

    query = query.order_by(*sorts).limit(limit).offset(offset)

    return jsonify(
        {
            "status": "ok",
            "data": [
                object_schema.from_orm(_).dict(exclude=exclude) for _ in query.all()
            ],
            "page": (offset // limit) + 1,
            "size": limit,
            "count": query.count(),
        }
    )


@app_api.route("/api/v1/")
def api_status():
    return jsonify({"status": "ok"})


@app_api.route("/api/v1/namespace/")
def api_list_namespaces():
    conda_store = get_conda_store()
    auth = get_auth()

    orm_namespaces = auth.filter_namespaces(api.list_namespaces(conda_store.db))
    return paginated_api_response(
        orm_namespaces,
        schema.Namespace,
        allowed_sort_bys={
            "name": orm.Namespace.name,
        },
        default_sort_by=["name"],
    )


@app_api.route("/api/v1/environment/")
def api_list_environments():
    conda_store = get_conda_store()
    auth = get_auth()

    search = request.args.get("search")

    orm_environments = auth.filter_environments(
        api.list_environments(conda_store.db, search=search)
    )
    return paginated_api_response(
        orm_environments,
        schema.Environment,
        exclude={"current_build"},
        allowed_sort_bys={
            "namespace": orm.Namespace.name,
            "name": orm.Environment.name,
        },
        default_sort_by=["namespace", "name"],
    )


@app_api.route("/api/v1/environment/<namespace>/<name>/", methods=["GET"])
def api_get_environment(namespace, name):
    conda_store = get_conda_store()
    auth = get_auth()

    auth.authorize_request(
        f"{namespace}/{name}", {Permissions.ENVIRONMENT_READ}, require=True
    )

    environment = api.get_environment(conda_store.db, namespace=namespace, name=name)
    if environment is None:
        return jsonify({"status": "error", "error": "environment does not exist"}), 404

    return jsonify(
        {
            "status": "ok",
            "data": schema.Environment.from_orm(environment).dict(
                exclude={"current_build"}
            ),
        }
    )


@app_api.route("/api/v1/environment/<namespace>/<name>/", methods=["PUT"])
def api_update_environment_build(namespace, name):
    conda_store = get_conda_store()
    auth = get_auth()

    auth.authorize_request(
        f"{namespace}/{name}", {Permissions.ENVIRONMENT_UPDATE}, require=True
    )

    data = request.json
    if "buildId" not in data:
        return jsonify({"status": "error", "message": "build id not specificated"}), 400

    try:
        build_id = data["buildId"]
        conda_store.update_environment_build(namespace, name, build_id)
    except utils.CondaStoreError as e:
        return e.response

    return jsonify({"status": "ok"})


@app_api.route("/api/v1/environment/<namespace>/<name>/", methods=["DELETE"])
def api_delete_environment(namespace, name):
    conda_store = get_conda_store()
    auth = get_auth()

    auth.authorize_request(
        f"{namespace}/{name}", {Permissions.ENVIRONMENT_DELETE}, require=True
    )

    conda_store.delete_environment(namespace, name)
    return jsonify({"status": "ok"})


@app_api.route("/api/v1/specification/", methods=["POST"])
def api_post_specification():
    conda_store = get_conda_store()
    try:
        specification = schema.CondaSpecification.parse_obj(request.json)
        api.post_specification(conda_store, specification)
        return jsonify({"status": "ok"})
    except pydantic.ValidationError as e:
        return jsonify({"status": "error", "error": e.errors()}), 400


@app_api.route("/api/v1/build/", methods=["GET"])
def api_list_builds():
    conda_store = get_conda_store()
    auth = get_auth()

    orm_builds = auth.filter_builds(api.list_builds(conda_store.db))
    return paginated_api_response(
        orm_builds,
        schema.Build,
        exclude={"specification", "packages"},
        allowed_sort_bys={
            "id": orm.Build.id,
        },
        default_sort_by=["id"],
    )


@app_api.route("/api/v1/build/<build_id>/", methods=["GET"])
def api_get_build(build_id):
    conda_store = get_conda_store()
    auth = get_auth()

    build = api.get_build(conda_store.db, build_id)
    if build is None:
        return jsonify({"status": "error", "error": "build id does not exist"}), 404

    auth.authorize_request(
        f"{build.environment.namespace.name}/{build.environment.name}",
        {Permissions.ENVIRONMENT_READ},
        require=True,
    )

    return jsonify({"status": "ok", "data": schema.Build.from_orm(build).dict()})


@app_api.route("/api/v1/build/<build_id>/", methods=["PUT"])
def api_put_build(build_id):
    conda_store = get_conda_store()
    auth = get_auth()

    build = api.get_build(conda_store.db, build_id)
    if build is None:
        return jsonify({"status": "error", "error": "build id does not exist"}), 404

    auth.authorize_request(
        f"{build.environment.namespace.name}/{build.environment.name}",
        {Permissions.ENVIRONMENT_READ},
        require=True,
    )

    conda_store.create_build(build.environment_id, build.specification.sha256)
    return jsonify({"status": "ok", "message": "rebuild triggered"})


@app_api.route("/api/v1/build/<build_id>/", methods=["DELETE"])
def api_delete_build(build_id):
    conda_store = get_conda_store()
    auth = get_auth()

    build = api.get_build(conda_store.db, build_id)
    if build is None:
        return jsonify({"status": "error", "error": "build id does not exist"}), 404

    auth.authorize_request(
        f"{build.environment.namespace.name}/{build.environment.name}",
        {Permissions.BUILD_DELETE},
        require=True,
    )

    conda_store.delete_build(build_id)
    return jsonify({"status": "ok"})


@app_api.route("/api/v1/build/<build_id>/logs/", methods=["GET"])
def api_get_build_logs(build_id):
    conda_store = get_conda_store()
    auth = get_auth()

    build = api.get_build(conda_store.db, build_id)
    if build is None:
        return jsonify({"status": "error", "error": "build id does not exist"}), 404

    auth.authorize_request(
        f"{build.environment.namespace.name}/{build.environment.name}",
        {Permissions.ENVIRONMENT_DELETE},
        require=True,
    )

    return redirect(conda_store.storage.get_url(build.log_key))


@app_api.route("/api/v1/channel/", methods=["GET"])
def api_list_channels():
    conda_store = get_conda_store()

    orm_channels = api.list_conda_channels(conda_store.db)
    return paginated_api_response(
        orm_channels,
        schema.CondaChannel,
        allowed_sort_bys={"name": orm.CondaChannel.name},
        default_sort_by=["name"],
    )


@app_api.route("/api/v1/package/", methods=["GET"])
def api_list_packages():
    conda_store = get_conda_store()

    search = request.args.get("search")
    build = request.args.get("build")

    orm_packages = api.list_conda_packages(conda_store.db, search=search, build=build)
    return paginated_api_response(
        orm_packages,
        schema.CondaPackage,
        allowed_sort_bys={
            "channel": orm.CondaChannel.name,
            "name": orm.CondaPackage.name,
        },
        default_sort_by=["channel", "name"],
    )
