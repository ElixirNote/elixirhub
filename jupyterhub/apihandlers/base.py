"""Base API handlers"""
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import json
from functools import lru_cache
from http.client import responses
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlunparse

from sqlalchemy.exc import SQLAlchemyError
from tornado import web

from .. import orm
from ..handlers import BaseHandler
from ..utils import get_browser_protocol
from ..utils import isoformat
from ..utils import url_path_join

PAGINATION_MEDIA_TYPE = "application/jupyterhub-pagination+json"


class APIHandler(BaseHandler):
    """Base class for API endpoints

    Differences from page handlers:

    - JSON responses and errors
    - strict referer checking for Cookie-authenticated requests
    - strict content-security-policy
    - methods for REST API models
    """

    # accept token-based authentication for API requests
    _accept_token_auth = True

    @property
    def content_security_policy(self):
        return '; '.join([super().content_security_policy, "default-src 'none'"])

    def get_content_type(self):
        return 'application/json'

    @property
    @lru_cache()
    def accepts_pagination(self):
        """Return whether the client accepts the pagination preview media type"""
        accept_header = self.request.headers.get("Accept", "")
        if not accept_header:
            return False
        accepts = {s.strip().lower() for s in accept_header.strip().split(",")}
        return PAGINATION_MEDIA_TYPE in accepts

    def check_referer(self):
        """Check Origin for cross-site API requests.

        Copied from WebSocket with changes:

        - allow unspecified host/referer (e.g. scripts)
        """
        host_header = self.app.forwarded_host_header or "Host"
        host = self.request.headers.get(host_header)
        if host and "," in host:
            host = host.split(",", 1)[0].strip()
        referer = self.request.headers.get("Referer")

        # If no header is provided, assume it comes from a script/curl.
        # We are only concerned with cross-site browser stuff here.
        if not host:
            self.log.warning("Blocking API request with no host")
            return False
        if not referer:
            self.log.warning("Blocking API request with no referer")
            return False

        proto = get_browser_protocol(self.request)

        full_host = f"{proto}://{host}{self.hub.base_url}"
        host_url = urlparse(full_host)
        referer_url = urlparse(referer)
        # resolve default ports for http[s]
        referer_port = referer_url.port or (
            443 if referer_url.scheme == 'https' else 80
        )
        host_port = host_url.port or (443 if host_url.scheme == 'https' else 80)
        if (
            referer_url.scheme != host_url.scheme
            or referer_url.hostname != host_url.hostname
            or referer_port != host_port
            or not (referer_url.path + "/").startswith(host_url.path)
        ):
            self.log.warning(
                f"Blocking Cross Origin API request.  Referer: {referer},"
                f" {host_header}: {host}, Host URL: {full_host}",
            )
            return False
        return True

    def check_post_content_type(self):
        """Check request content-type, e.g. for cross-site POST requests

        Cross-site POST via form will include content-type
        """
        content_type = self.request.headers.get("Content-Type")
        if not content_type:
            # not specified, e.g. from a script
            return True

        # parse content type for application/json
        fields = content_type.lower().split(";")
        if not any(f.lstrip().startswith("application/json") for f in fields):
            self.log.warning(f"Not allowing POST with content-type: {content_type}")
            return False

        return True

    def get_current_user_cookie(self):
        """Extend get_user_cookie to add checks for CORS"""
        cookie_user = super().get_current_user_cookie()
        # CORS checks for cookie-authentication
        # check these only if there is a cookie user,
        # avoiding misleading "Blocking Cross Origin" messages
        # when there's no cookie set anyway.
        if cookie_user:
            if not self.check_referer():
                return None
            if (
                self.request.method.upper() == 'POST'
                and not self.check_post_content_type()
            ):
                return None
        return cookie_user

    def get_json_body(self):
        """Return the body of the request as JSON data."""
        if not self.request.body:
            return None
        body = self.request.body.strip().decode('utf-8')
        try:
            model = json.loads(body)
        except Exception:
            self.log.debug("Bad JSON: %r", body)
            self.log.error("Couldn't parse JSON", exc_info=True)
            raise web.HTTPError(400, 'Invalid JSON in body of request')
        return model

    def write_error(self, status_code, **kwargs):
        """Write JSON errors instead of HTML"""
        exc_info = kwargs.get('exc_info')
        message = ''
        exception = None
        status_message = responses.get(status_code, 'Unknown Error')
        if exc_info:
            exception = exc_info[1]
            # get the custom message, if defined
            try:
                message = exception.log_message % exception.args
            except Exception:
                pass

            # construct the custom reason, if defined
            reason = getattr(exception, 'reason', '')
            if reason:
                status_message = reason

        if exception and isinstance(exception, SQLAlchemyError):
            try:
                exception_str = str(exception)
                self.log.warning(
                    "Rolling back session due to database error %s", exception_str
                )
            except Exception:
                self.log.warning(
                    "Rolling back session due to database error %s", type(exception)
                )
            self.db.rollback()

        self.set_header('Content-Type', 'application/json')
        if isinstance(exception, web.HTTPError):
            # allow setting headers from exceptions
            # since exception handler clears headers
            headers = getattr(exception, 'headers', None)
            if headers:
                for key, value in headers.items():
                    self.set_header(key, value)
            # Content-Length must be recalculated.
            self.clear_header('Content-Length')

        self.write(
            json.dumps({'status': status_code, 'message': message or status_message})
        )

    def server_model(self, spawner):
        """Get the JSON model for a Spawner
        Assume server permission already granted"""
        model = {
            'name': spawner.name,
            'last_activity': isoformat(spawner.orm_spawner.last_activity),
            'started': isoformat(spawner.orm_spawner.started),
            'pending': spawner.pending,
            'ready': spawner.ready,
            'url': url_path_join(spawner.user.url, spawner.name, '/'),
            'user_options': spawner.user_options,
            'progress_url': spawner._progress_url,
        }
        scope_filter = self.get_scope_filter('admin:server_state')
        if scope_filter(spawner, kind='server'):
            model['state'] = spawner.get_state()
        return model

    def token_model(self, token):
        """Get the JSON model for an APIToken"""

        if token.user:
            owner_key = 'user'
            owner = token.user.name

        else:
            owner_key = 'service'
            owner = token.service.name

        model = {
            owner_key: owner,
            'id': token.api_id,
            'kind': 'api_token',
            'roles': [r.name for r in token.roles],
            'created': isoformat(token.created),
            'last_activity': isoformat(token.last_activity),
            'expires_at': isoformat(token.expires_at),
            'note': token.note,
            'session_id': token.session_id,
            'oauth_client': token.oauth_client.description
            or token.oauth_client.identifier,
        }
        return model

    def _filter_model(self, model, access_map, entity, kind, keys=None):
        """
        Filter the model based on the available scopes and the entity requested for.
        If keys is a dictionary, update it with the allowed keys for the model.
        """
        allowed_keys = set()
        for scope in access_map:
            scope_filter = self.get_scope_filter(scope)
            if scope_filter(entity, kind=kind):
                allowed_keys |= access_map[scope]
        model = {key: model[key] for key in allowed_keys if key in model}
        if isinstance(keys, set):
            keys.update(allowed_keys)
        return model

    def user_model(self, user):
        """Get the JSON model for a User object"""
        if isinstance(user, orm.User):
            user = self.users[user.id]
        model = {
            'kind': 'user',
            'name': user.name,
            'admin': user.admin,
            'roles': [r.name for r in user.roles],
            'groups': [g.name for g in user.groups],
            'server': user.url if user.running else None,
            'pending': None,
            'created': isoformat(user.created),
            'last_activity': isoformat(user.last_activity),
            'auth_state': None,  # placeholder, filled in later
        }
        access_map = {
            'read:users': {
                'kind',
                'name',
                'admin',
                'roles',
                'groups',
                'server',
                'pending',
                'created',
                'last_activity',
            },
            'read:users:name': {'kind', 'name', 'admin'},
            'read:users:groups': {'kind', 'name', 'groups'},
            'read:users:activity': {'kind', 'name', 'last_activity'},
            'read:servers': {'kind', 'name', 'servers'},
            'read:roles:users': {'kind', 'name', 'roles', 'admin'},
            'admin:auth_state': {'kind', 'name', 'auth_state'},
        }
        allowed_keys = set()
        model = self._filter_model(
            model, access_map, user, kind='user', keys=allowed_keys
        )
        if model:
            if '' in user.spawners and 'pending' in allowed_keys:
                model['pending'] = user.spawners[''].pending

            servers = model['servers'] = {}
            scope_filter = self.get_scope_filter('read:servers')
            for name, spawner in user.spawners.items():
                # include 'active' servers, not just ready
                # (this includes pending events)
                if spawner.active and scope_filter(spawner, kind='server'):
                    servers[name] = self.server_model(spawner)
            if not servers and 'servers' not in allowed_keys:
                # omit servers if no access
                # leave present and empty
                # if request has access to read servers in general
                model.pop('servers')
        return model

    def group_model(self, group):
        """Get the JSON model for a Group object"""
        model = {
            'kind': 'group',
            'name': group.name,
            'roles': [r.name for r in group.roles],
            'users': [u.name for u in group.users],
        }
        access_map = {
            'read:groups': {'kind', 'name', 'users'},
            'read:groups:name': {'kind', 'name'},
            'read:roles:groups': {'kind', 'name', 'roles'},
        }
        model = self._filter_model(model, access_map, group, 'group')
        return model

    def service_model(self, service):
        """Get the JSON model for a Service object"""
        model = {
            'kind': 'service',
            'name': service.name,
            'roles': [r.name for r in service.roles],
            'admin': service.admin,
            'url': getattr(service, 'url', ''),
            'prefix': service.server.base_url if getattr(service, 'server', '') else '',
            'command': getattr(service, 'command', ''),
            'pid': service.proc.pid if getattr(service, 'proc', '') else 0,
            'info': getattr(service, 'info', ''),
            'display': getattr(service, 'display', ''),
        }
        access_map = {
            'read:services': {
                'kind',
                'name',
                'admin',
                'url',
                'prefix',
                'command',
                'pid',
                'info',
                'display',
            },
            'read:services:name': {'kind', 'name', 'admin'},
            'read:roles:services': {'kind', 'name', 'roles', 'admin'},
        }
        model = self._filter_model(model, access_map, service, 'service')
        return model

    _user_model_types = {
        'name': str,
        'admin': bool,
        'groups': list,
        'roles': list,
        'auth_state': dict,
    }

    _group_model_types = {'name': str, 'users': list, 'roles': list}

    def _check_model(self, model, model_types, name):
        """Check a model provided by a REST API request

        Args:
            model (dict): user-provided model
            model_types (dict): dict of key:type used to validate types and keys
            name (str): name of the model, used in error messages
        """
        if not isinstance(model, dict):
            raise web.HTTPError(400, "Invalid JSON data: %r" % model)
        if not set(model).issubset(set(model_types)):
            raise web.HTTPError(400, "Invalid JSON keys: %r" % model)
        for key, value in model.items():
            if not isinstance(value, model_types[key]):
                raise web.HTTPError(
                    400,
                    "%s.%s must be %s, not: %r"
                    % (name, key, model_types[key], type(value)),
                )

    def _check_user_model(self, model):
        """Check a request-provided user model from a REST API"""
        self._check_model(model, self._user_model_types, 'user')
        for username in model.get('users', []):
            if not isinstance(username, str):
                raise web.HTTPError(
                    400, ("usernames must be str, not %r", type(username))
                )

    def _check_group_model(self, model):
        """Check a request-provided group model from a REST API"""
        self._check_model(model, self._group_model_types, 'group')
        for groupname in model.get('groups', []):
            if not isinstance(groupname, str):
                raise web.HTTPError(
                    400, ("group names must be str, not %r", type(groupname))
                )

    def get_api_pagination(self):
        default_limit = self.settings["api_page_default_limit"]
        max_limit = self.settings["api_page_max_limit"]
        if not self.accepts_pagination:
            # if new pagination Accept header is not used,
            # default to the higher max page limit to reduce likelihood
            # of missing users due to pagination in code that hasn't been updated
            default_limit = max_limit
        offset = self.get_argument("offset", None)
        limit = self.get_argument("limit", default_limit)
        try:
            offset = abs(int(offset)) if offset is not None else 0
            limit = abs(int(limit))
            if limit > max_limit:
                limit = max_limit
            if limit < 1:
                limit = 1
        except Exception as e:
            raise web.HTTPError(
                400, "Invalid argument type, offset and limit must be integers"
            )
        return offset, limit

    def paginated_model(self, items, offset, limit, total_count):
        """Return the paginated form of a collection (list or dict)

        A dict with { items: [], _pagination: {}}
        instead of a single list (or dict).

        pagination info includes the current offset and limit,
        the total number of results for the query,
        and information about how to build the next page request
        if there is one.
        """
        next_offset = offset + limit
        data = {
            "items": items,
            "_pagination": {
                "offset": offset,
                "limit": limit,
                "total": total_count,
                "next": None,
            },
        }
        if next_offset < total_count:
            # if there's a next page
            next_url_parsed = urlparse(self.request.full_url())
            query = parse_qs(next_url_parsed.query)
            query['offset'] = [next_offset]
            query['limit'] = [limit]
            next_url_parsed = next_url_parsed._replace(
                query=urlencode(query, doseq=True)
            )
            next_url = urlunparse(next_url_parsed)
            data["_pagination"]["next"] = {
                "offset": next_offset,
                "limit": limit,
                "url": next_url,
            }
        return data

    def options(self, *args, **kwargs):
        self.finish()


class API404(APIHandler):
    """404 for API requests

    Ensures JSON 404 errors for malformed URLs
    """

    async def prepare(self):
        await super().prepare()
        raise web.HTTPError(404)
