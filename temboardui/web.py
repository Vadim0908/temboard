# coding: utf-8
from __future__ import absolute_import

import functools
import logging
import os

from tornado import web as tornadoweb
from tornado.concurrent import run_on_executor
from tornado.gen import coroutine
from tornado.web import (
    Application as TornadoApplication,
    HTTPError,
    RequestHandler,
)
from tornado.template import Loader as TemplateLoader

from .application import (
    get_instance,
    get_role_by_cookie,
)
from .model import Session as DBSession
from .temboardclient import (
    TemboardError,
    temboard_profile,
    temboard_get_notifications,
)


logger = logging.getLogger(__name__)


class Response(object):
    def __init__(
            self, status_code=200, headers=None, secure_cookies=None,
            body=None,):
        self.status_code = status_code
        self.headers = headers or {}
        self.secure_cookies = secure_cookies or {}
        self.body = body or u''


class Redirect(Response, Exception):
    def __init__(self, location, permanent=False):
        super(Redirect, self).__init__(
            status_code=301 if permanent else 302,
            headers={'Location': location},
            body=u'Redirected to %s' % location,
        )


class TemplateRenderer(object):
    # Flask-like HTML render function, without thread local.

    def __init__(self, path):
        self.loader = TemplateLoader(path)

    def __call__(self, template, **data):
        return Response(body=self.loader.load(template).generate(**data))


template_path = os.path.realpath(__file__ + '/../templates')
render_template = TemplateRenderer(template_path)


class CallableHandler(RequestHandler):
    # Adapt flask-like callable in Tornado Handler API.

    @property
    def executor(self):
        # To enable @run_on_executor methods, we must have executor property.
        return self.application.executor

    def initialize(self, callable_, methods=None, logger=None):
        self.callable_ = callable_
        self.logger = logger or logging.getLogger(__name__)
        self.request.handler = self
        self.request.config = self.application.config
        self.SUPPORTED_METHODS = methods or ['GET']

    def get_current_user(self):
        cookie = self.get_secure_cookie('temboard')
        if not cookie:
            return

        try:
            return get_role_by_cookie(self.db_session, cookie)
        except Exception as e:
            self.logger.error("Failed to get role from cookie: %s ", e)

    @run_on_executor
    def prepare(self):
        # This should be middlewares
        self.request.db_session = self.db_session = DBSession()
        self.request.current_user = self.current_user

    @run_on_executor
    def on_finish(self):
        # This should be middlewares
        self.request.db_session.commit()
        self.request.db_session.close()
        del self.request.db_session

    @coroutine
    def get(self, *args, **kwargs):
        try:
            response = yield self.callable_(self.request, *args, **kwargs)
        except Redirect as response:
            pass

        if response is None:
            response = u''
        if isinstance(response, unicode):
            response = Response(body=response)
        self.write_response(response)

    # Let's use one handler for all supported methods.
    post = get

    def write_response(self, response):
        # Should be in a middleware.
        if response.status_code in (301, 302, 401):
            response.secure_cookies['referer_uri'] = self.request.uri

        self.set_status(response.status_code)
        for k, v in response.headers.items():
            if not isinstance(v, list):
                v = [v]
            for v1 in v:
                self.add_header(k, v1)

        for k, v in response.secure_cookies.items():
            self.set_secure_cookie(k, v, expires_days=30)

        self.finish(response.body)


class InstanceHelper(object):
    # This helper class implements all operations related to instance dedicated
    # request.

    URL_PREFIX = r'/server/(.*)/([0-9]{1,5})'

    @classmethod
    def add_middleware(cls, callable_):
        # Wraps an HTTP handler callable related to a Postgres instance

        @functools.wraps(callable_)
        def middleware(request, address, port, *args):
            # Swallow adddress and port arguments.
            request.instance = cls(request)
            request.instance.fetch_instance(address, port)
            return callable_(request, *args)

        return middleware

    def __init__(self, request):
        self.request = request
        self._xsession = False

    def __getattr__(self, name):
        return getattr(self.instance, name)

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.instance.hostname)

    def fetch_instance(self, address, port):
        self.instance = get_instance(self.request.db_session, address, port)
        if not self.instance:
            raise HTTPError(404)

    @property
    def cookie_name(self):
        return 'temboard_%s_%s' % (
            self.instance.agent_address, self.instance.agent_port,
        )

    @property
    def xsession(self):
        if self._xsession is False:
            self._xsession = self.request.handler.get_secure_cookie(
                self.cookie_name)
        return self._xsession

    def redirect_login(self):
        login_url = "/server/%s/%s/login" % (
            self.instance.agent_address, self.instance.agent_port)
        raise Redirect(location=login_url)

    def get_xsession(self):
        if not self.xsession:
            self.redirect_login()
        return self.xsession

    def get_profile(self):
        try:
            return temboard_profile(
                self.request.config.temboard.ssl_ca_cert_file,
                self.instance.agent_address,
                self.instance.agent_port,
                self.get_xsession(),
            )
        except TemboardError as e:
            if 401 == e.code:
                self.redirect_login()
            logger.error('Instance error: %s', e)
            raise HTTPError(500)

    def get_notifications(self):
        return temboard_get_notifications(
            self.request.config.temboard.ssl_ca_cert_file,
            self.instance.agent_address,
            self.instance.agent_port,
            self.get_xsession(),
        )


class WebApplication(TornadoApplication):
    def __init__(self, *a, **kwargs):
        super(WebApplication, self).__init__(*a, **kwargs)

    def configure(self, **settings):
        # Runtime configuration of application.
        #
        # This way, we can initialize app at import time to register handlers.
        # Then configure it at run time once configuration is parsed.

        self.settings.update(settings)

        # This comme from Tornado's __init__
        if self.settings.get('debug'):
            self.settings.setdefault('autoreload', True)
            self.settings.setdefault('compiled_template_cache', False)
            self.settings.setdefault('static_hash_cache', False)
            self.settings.setdefault('serve_traceback', True)

    def route(self, url, methods=None, with_instance=False):
        # Implements flask-like route registration of a simple synchronous
        # callable.

        def decorator(func):
            logger_name = func.__module__ + '.' + func.__name__

            if with_instance:
                func = InstanceHelper.add_middleware(func)

            # run_on_executor searches for `executor` attribute of first
            # argument. Thus, we bind executor to application object for
            # run_on_executor, hardcode here app as the first argument using
            # partial, and swallow app argument in the wrapper.
            @run_on_executor
            def wrapper(app, *args):
                try:
                    return func(*args)
                except (HTTPError, Redirect):
                    raise
                except Exception:
                    # Since async traceback is useless, spit here traceback and
                    # just raise HTTP 500.
                    logger.exception("Unhandled Error:")
                    raise HTTPError(500)

            wrapper = functools.partial(wrapper, self)

            rules = [(
                url, CallableHandler, dict(
                    callable_=wrapper,
                    methods=methods or ['GET'],
                    logger=logging.getLogger(logger_name),
                ),
            )]
            self.add_rules(rules)
            return func

        return decorator

    def add_rules(self, rules):
        if hasattr(self, 'wildcard_router'):  # Tornado 4.5+
            self.wildcard_router.add_rules(rules)
        elif not self.handlers:
            self.add_handlers(r'.*$', rules)
        else:
            rules = [tornadoweb.URLSpec(*r) for r in rules]
            self.handlers[0][1].extend(rules)

    def instance_route(self, url, methods=None):
        # Helper to declare a route with instance URL prefix and middleware.
        return self.route(
            url=InstanceHelper.URL_PREFIX + url,
            methods=methods,
            with_instance=True,
        )


# Global app instance for registration of core handlers.
app = WebApplication()
# Hijack tornado.web access_log to log request in temboardui namespace.
tornadoweb.access_log = logging.getLogger('temboardui.access')