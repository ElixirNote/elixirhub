"""API handlers for administering the Hub itself"""
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import json
import sys

from tornado import web
from tornado.ioloop import IOLoop

from .._version import __version__
from ..scopes import needs_scope
from .base import APIHandler


class ShutdownAPIHandler(APIHandler):
    @needs_scope('shutdown')
    def post(self):
        """POST /api/shutdown triggers a clean shutdown

        POST (JSON) parameters:

        - servers: specify whether single-user servers should be terminated
        - proxy: specify whether the proxy should be terminated
        """
        from ..app import JupyterHub

        app = JupyterHub.instance()

        data = self.get_json_body()
        if data:
            if 'proxy' in data:
                proxy = data['proxy']
                if proxy not in {True, False}:
                    raise web.HTTPError(
                        400, "proxy must be true or false, got %r" % proxy
                    )
                app.cleanup_proxy = proxy
            if 'servers' in data:
                servers = data['servers']
                if servers not in {True, False}:
                    raise web.HTTPError(
                        400, "servers must be true or false, got %r" % servers
                    )
                app.cleanup_servers = servers

        # finish the request
        self.set_status(202)
        self.finish(json.dumps({"message": "Shutting down Hub"}))

        # stop the eventloop, which will trigger cleanup
        loop = IOLoop.current()
        loop.add_callback(loop.stop)


class RootAPIHandler(APIHandler):
    def get(self):
        """GET /api/ returns info about the Hub and its API.

        It is not an authenticated endpoint
        For now, it just returns the version of JupyterHub itself.
        """
        data = {'version': __version__}
        self.finish(json.dumps(data))


class InfoAPIHandler(APIHandler):
    @needs_scope('read:hub')
    def get(self):
        """GET /api/info returns detailed info about the Hub and its API.

        Currently, it returns information on the python version, spawner and authenticator.
        Since this information might be sensitive, it is an authenticated endpoint
        """

        def _class_info(typ):
            """info about a class (Spawner or Authenticator)"""
            info = {'class': f'{typ.__module__}.{typ.__name__}'}
            pkg = typ.__module__.split('.')[0]
            try:
                version = sys.modules[pkg].__version__
            except (KeyError, AttributeError):
                version = 'unknown'
            info['version'] = version
            return info

        data = {
            'version': __version__,
            'python': sys.version,
            'sys_executable': sys.executable,
            'spawner': _class_info(self.settings['spawner_class']),
            'authenticator': _class_info(self.authenticator.__class__),
        }
        self.finish(json.dumps(data))


default_handlers = [
    (r"/api/shutdown", ShutdownAPIHandler),
    (r"/api/?", RootAPIHandler),
    (r"/api/info", InfoAPIHandler),
]
